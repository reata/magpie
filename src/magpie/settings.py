import os

from dotenv import load_dotenv

load_dotenv()


GITHUB_ACCESS_TOKEN = ""

# ClickPy's public read-only ClickHouse instance. Every environment talks to it;
# these are overridable only so a moved host or rotated demo credentials are a
# config change rather than a code change.
CLICKHOUSE_HOST = "sql-clickhouse.clickhouse.com"
CLICKHOUSE_PORT = "8443"
CLICKHOUSE_USER = "demo"
CLICKHOUSE_PASSWORD = ""
CLICKHOUSE_DATABASE = "pypi"
CLICKHOUSE_SECURE = "true"


# override the settings above using environment variable
# (or .env file in developing environment)
for setting, new_value in os.environ.items():
    if setting.isupper() and setting in locals():
        locals()[setting] = new_value
