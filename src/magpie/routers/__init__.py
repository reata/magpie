"""Inbound HTTP routes, one module per resource group."""

#: How long responses are memoized. Both upstreams move on the order of a day,
#: so a shorter TTL would only spend more of their quota.
CACHE_TTL = 6 * 60 * 60
