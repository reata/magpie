"""Business logic between the routes and the clients.

Queries, response shaping and aggregation: the code that is neither HTTP (see
``magpie.routers``) nor transport (see ``magpie.clients``).

Answers are memoized in the services: how long data stays fresh is a property of
the data source, not of the HTTP response.
"""

#: ClickPy and GitHub are both refreshed about once a day, so a shorter TTL
#: would only spend more of their quota.
CACHE_TTL = 6 * 60 * 60
