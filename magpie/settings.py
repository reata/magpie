import os
from dotenv import load_dotenv

load_dotenv()


GITHUB_ACCESS_TOKEN = ""


# override the settings above using environment variable (or .env file in developing environment)
for setting, new_value in os.environ.items():
    if setting.isupper() and setting in locals():
        locals()[setting] = new_value
