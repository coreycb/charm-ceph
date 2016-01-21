#!/usr/bin/python
#
# Copyright 2015 Canonical Ltd.
#
import json

from charmhelpers.contrib.storage.linux.ceph import validator, \
    erasure_profile_exists, ErasurePool, set_pool_quota, \
    pool_set, snapshot_pool, remove_pool_snapshot, create_erasure_profile, \
    ReplicatedPool, rename_pool, Pool

from charmhelpers.core.hookenv import (
    log,
    DEBUG,
    INFO,
    ERROR,
)

from charmhelpers.contrib.storage.linux.ceph import (
    pool_exists,
    delete_pool)

# This comes from http://docs.ceph.com/docs/master/rados/operations/pools/
# This should do a decent job of preventing people from passing in bad values.
# It will give a useful error message
POOL_KEYS = {
    # "Ceph Key Name": [Python type, [Valid Range]]
    "size": [int],
    "min_size": [int],
    "crash_replay_interval": [int],
    "pgp_num": [int],  # = or < pg_num
    "crush_ruleset": [int],
    "hashpspool": [bool],
    "nodelete": [bool],
    "nopgchange": [bool],
    "nosizechange": [bool],
    "write_fadvise_dontneed": [bool],
    "noscrub": [bool],
    "nodeep-scrub": [bool],
    "hit_set_type": [basestring, ["bloom", "explicit_hash",
                                  "explicit_object"]],
    "hit_set_count": [int, [1, 1]],
    "hit_set_period": [int],
    "hit_set_fpp": [float, [0.0, 1.0]],
    "cache_target_dirty_ratio": [float],
    "cache_target_dirty_high_ratio": [float],
    "cache_target_full_ratio": [float],
    "target_max_bytes": [int],
    "target_max_objects": [int],
    "cache_min_flush_age": [int],
    "cache_min_evict_age": [int],
    "fast_read": [bool],
}

CEPH_BUCKET_TYPES = [
    'osd',
    'host',
    'chassis',
    'rack',
    'row',
    'pdu',
    'pod',
    'room',
    'datacenter',
    'region',
    'root'
]


def decode_req_encode_rsp(f):
    """Decorator to decode incoming requests and encode responses."""

    def decode_inner(req):
        return json.dumps(f(json.loads(req)))

    return decode_inner


@decode_req_encode_rsp
def process_requests(reqs):
    """Process Ceph broker request(s).

    This is a versioned api. API version must be supplied by the client making
    the request.
    """
    request_id = reqs.get('request-id')
    try:
        version = reqs.get('api-version')
        if version == 1:
            log('Processing request {}'.format(request_id), level=DEBUG)
            resp = process_requests_v1(reqs['ops'])
            if request_id:
                resp['request-id'] = request_id

            return resp
    except Exception as exc:
        log(str(exc), level=ERROR)
        msg = ("Unexpected error occurred while processing requests: %s" %
               reqs)
        log(msg, level=ERROR)
        return {'exit-code': 1, 'stderr': msg}

    msg = ("Missing or invalid api version (%s)" % version)
    resp = {'exit-code': 1, 'stderr': msg}
    if request_id:
        resp['request-id'] = request_id

    return resp


def handle_create_erasure_profile(request, service):
    # "local" | "shec" or it defaults to "jerasure"
    erasure_type = request.get('erasure-type')
    # "host" | "rack" or it defaults to "host"  # Any valid Ceph bucket
    failure_domain = request.get('failure-domain')
    name = request.get('name')
    k = request.get('k')
    m = request.get('m')
    l = request.get('l')

    if failure_domain not in CEPH_BUCKET_TYPES:
        msg = "failure-domain must be one of {}".format(CEPH_BUCKET_TYPES)
        log(msg, level=ERROR)
        return {'exit-code': 1, 'stderr': msg}

    create_erasure_profile(service=service, erasure_plugin_name=erasure_type,
                           profile_name=name, failure_domain=failure_domain,
                           data_chunks=k, coding_chunks=m, locality=l)


def handle_erasure_pool(request, service):
    pool_name = request.get('name')
    erasure_profile = request.get('erasure-profile')
    quota = request.get('max-bytes')

    if erasure_profile is None:
        erasure_profile = "default-canonical"

    # Check for missing params
    if pool_name is None:
        msg = "Missing parameter. name is required for the pool"
        log(msg, level=ERROR)
        return {'exit-code': 1, 'stderr': msg}

    # TODO: Default to 6/3 erasure coding. I believe this requires min 9 osds
    if not erasure_profile_exists(service=service, name=erasure_profile):
        # TODO: Fail and tell them to create the profile or default
        msg = "erasure-profile {} does not exist.  Please create it with: " \
              "create-erasure-profile".format(erasure_profile)
        log(msg, level=ERROR)
        return {'exit-code': 1, 'stderr': msg}
        pass
    pool = ErasurePool(service=service, name=pool_name,
                       erasure_code_profile=erasure_profile)
    # Ok make the erasure pool
    if not pool_exists(service=service, name=pool_name):
        log("Creating pool '%s' (erasure_profile=%s)" % (pool,
                                                         erasure_profile),
            level=INFO)
        pool.create()

    # Set a quota if requested
    if quota is not None:
        set_pool_quota(service=service, pool_name=pool_name, max_bytes=quota)


def handle_replicated_pool(request, service):
    pool_name = request.get('name')
    replicas = request.get('replicas')
    quota = request.get('max-bytes')

    # Check for missing params
    if pool_name is None or replicas is None:
        msg = "Missing parameter. name and replicas are required"
        log(msg, level=ERROR)
        return {'exit-code': 1, 'stderr': msg}

    pool = ReplicatedPool(service=service, name=pool_name, replicas=replicas)
    if not pool_exists(service=service, name=pool_name):
        log("Creating pool '%s' (replicas=%s)" % (pool, replicas),
            level=INFO)
        pool.create()
    else:
        log("Pool '%s' already exists - skipping create" % pool,
            level=DEBUG)

    # Set a quota if requested
    if quota is not None:
        set_pool_quota(service=service, pool_name=pool_name, max_bytes=quota)


def handle_create_cache_tier(request, service):
    # mode = "writeback" | "readonly"
    storage_pool = request.get('cold-pool')
    cache_pool = request.get('hot-pool')
    cache_mode = request.get('mode')

    if cache_mode is None:
        cache_mode = "writeback"

    # cache and storage pool must exist first
    if not pool_exists(service=service, name=storage_pool) or not pool_exists(
            service=service, name=cache_pool):
        msg = "cold-pool: {} and hot-pool: {} must exist. Please create " \
              "them first".format(storage_pool, cache_pool)
        log(msg, level=ERROR)
        return {'exit-code': 1, 'stderr': msg}
    p = Pool(service=service, name=storage_pool)
    p.add_cache_tier(cache_pool=cache_pool, mode=cache_mode)


def handle_remove_cache_tier(request, service):
    storage_pool = request.get('cold-pool')
    cache_pool = request.get('hot-pool')
    # cache and storage pool must exist first
    if not pool_exists(service=service, name=storage_pool) or not pool_exists(
            service=service, name=cache_pool):
        msg = "cold-pool: {} or hot-pool: {} doesn't exist. Not " \
              "deleting cache tier".format(storage_pool, cache_pool)
        log(msg, level=ERROR)
        return {'exit-code': 1, 'stderr': msg}

    pool = Pool(name=storage_pool, service=service)
    pool.remove_cache_tier(cache_pool=cache_pool)


def handle_set_pool_value(request, service):
    # Set arbitrary pool values
    params = {'pool': request.get('name'),
              'key': request.get('key'),
              'value': request.get('value')}
    if params['key'] not in POOL_KEYS:
        msg = "Invalid key '%s'" % params['key']
        log(msg, level=ERROR)
        return {'exit-code': 1, 'stderr': msg}

    # Get the validation method
    validator_params = POOL_KEYS[params['key']]
    if len(validator_params) is 1:
        # Validate that what the user passed is actually legal per Ceph's rules
        validator(params['value'], validator_params[0])
    else:
        # Validate that what the user passed is actually legal per Ceph's rules
        validator(params['value'], validator_params[0], validator_params[1])
    # Set the value
    pool_set(service=service, pool_name=params['pool'], key=params['key'],
             value=params['value'])


def process_requests_v1(reqs):
    """Process v1 requests.

    Takes a list of requests (dicts) and processes each one. If an error is
    found, processing stops and the client is notified in the response.

    Returns a response dict containing the exit code (non-zero if any
    operation failed along with an explanation).
    """
    log("Processing %s ceph broker requests" % (len(reqs)), level=INFO)
    for req in reqs:
        op = req.get('op')
        log("Processing op='%s'" % op, level=DEBUG)
        # Use admin client since we do not have other client key locations
        # setup to use them for these operations.
        svc = 'admin'
        if op == "create-pool":
            pool_type = req.get('pool-type')  # "replicated" | "erasure"

            # Default to replicated if pool_type isn't given
            if pool_type == 'erasure':
                handle_erasure_pool(request=req, service=svc)
            else:
                handle_replicated_pool(request=req, service=svc)
        elif op == "create-cache-tier":
            handle_create_cache_tier(request=req, service=svc)
        elif op == "remove-cache-tier":
            handle_remove_cache_tier(request=req, service=svc)
        elif op == "create-erasure-profile":
            handle_create_erasure_profile(request=req, service=svc)
        elif op == "delete-pool":
            pool = req.get('name')
            delete_pool(service=svc, name=pool)
        elif op == "rename-pool":
            old_name = req.get('name')
            new_name = req.get('new-name')
            rename_pool(service=svc, old_name=old_name, new_name=new_name)
        elif op == "snapshot-pool":
            pool = req.get('name')
            snapshot_name = req.get('snapshot-name')
            snapshot_pool(service=svc, pool_name=pool,
                          snapshot_name=snapshot_name)
        elif op == "remove-pool-snapshot":
            pool = req.get('name')
            snapshot_name = req.get('snapshot-name')
            remove_pool_snapshot(service=svc, pool_name=pool,
                                 snapshot_name=snapshot_name)
        elif op == "set-pool-value":
            handle_set_pool_value(request=req, service=svc)
        else:
            msg = "Unknown operation '%s'" % op
            log(msg, level=ERROR)
            return {'exit-code': 1, 'stderr': msg}

    return {'exit-code': 0}
