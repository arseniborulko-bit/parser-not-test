import os
import importlib

os.environ['TEST_ASIN'] = 'https://www.amazon.com/dp/B0CZ767JDG'
config = importlib.import_module('config')
config = importlib.reload(config)
print(config.resolve_asins_from_environment(['B0B917WMSF', 'B09STLBKWV']))
