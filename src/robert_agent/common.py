import json


def emit(payload, exit_code=0):
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return exit_code
