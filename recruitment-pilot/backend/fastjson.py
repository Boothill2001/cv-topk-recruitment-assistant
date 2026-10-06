"""Large immutable snapshots need fast JSON encoding; wire values remain ordinary JSON."""
import orjson

def loads(value):return orjson.loads(value)
def dumps(value):return orjson.dumps(value).decode('utf-8')
