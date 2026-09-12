"""Star history: page through GitHub's stargazers, then aggregate by day.

GitHub serves stargazers one page at a time and the dashboard wants a cumulative
daily series, so both the pagination and the date-series construction live here
-- neither belongs to the route, which only knows about the HTTP response.
"""

from collections import Counter
from datetime import date, datetime, timedelta, timezone

from async_lru import alru_cache

from magpie.clients.http import http
from magpie.errors import RemoteError
from magpie.services import CACHE_TTL
from magpie.settings import GITHUB_ACCESS_TOKEN

PER_PAGE = 100


def _today() -> date:
    """Today in UTC -- GitHub's timestamps are UTC, and tests freeze this."""
    return datetime.now(timezone.utc).date()


@alru_cache(maxsize=64, ttl=CACHE_TTL)
async def star_history(repo: str) -> list[dict]:
    """Cumulative daily star counts, oldest first.

    The series runs from the first star through today: every day has an entry, so
    a day nobody starred carries ``star_cnt: 0`` and the previous cumulative
    count, and the tail stays flat instead of stopping at the last star.

    An empty list for a repository without stars: there is nothing to aggregate
    and no date for the series to start from. Paging through every stargazer is the
    expensive part, so the result is cached here.
    """
    starred_at = await _stargazer_timestamps(repo)
    if not starred_at:
        return []

    star_dt = [datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").date() for ts in starred_at]
    counts = Counter(star_dt)

    # min/max rather than the first and last entry: the range must not depend on
    # the order GitHub returned the pages in. max() against today keeps a
    # timestamp a few seconds ahead of our clock from emptying the series.
    first, last = min(star_dt), max(max(star_dt), _today())

    series = []
    cumulative = 0
    day = first
    while day <= last:
        count = counts.get(day, 0)
        cumulative += count
        series.append({"date": str(day), "star_cnt": count, "star_cum_cnt": cumulative})
        day += timedelta(days=1)
    return series


async def _stargazer_timestamps(repo: str) -> list[str]:
    """Every ``starred_at`` for ``repo``, page by page."""
    page = 1
    starred_at: list[str] = []
    while True:
        proxy = await http.get(
            f"https://api.github.com/repos/{repo}/stargazers"
            f"?per_page={PER_PAGE}&page={page}",
            headers={
                "Accept": "application/vnd.github.v3.star+json",
                "Authorization": f"token {GITHUB_ACCESS_TOKEN}",
            },
        )
        if not proxy.is_success:
            raise RemoteError(f"github returned HTTP {proxy.status_code}")
        timestamps = [stargazer["starred_at"] for stargazer in proxy.json()]
        starred_at.extend(timestamps)
        page += 1
        if len(timestamps) < PER_PAGE:
            return starred_at
