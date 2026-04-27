import importlib.util
import sys

packages = {
    "flask": "flask",
    "flask_cors": "flask-cors",
    "requests": "requests",
    "netschoolapi": "netschoolapi",
    "nest_asyncio": "nest-asyncio",
    "httpcore": "httpcore",
}

missing = []
for module, package in packages.items():
    if importlib.util.find_spec(module) is None:
        missing.append(package)

if missing:
    print("Не установлены зависимости:")
    for p in missing:
        print(" -", p)
    print("\nУстановите командой:")
    print(sys.executable, "-m pip install", " ".join(missing))
    sys.exit(1)

print("Все зависимости установлены.")
