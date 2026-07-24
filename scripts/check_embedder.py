"""Quick diagnostic: what does embedder.embed return?"""
from hl_mem.components import make_embedder
from hl_mem.settings import Settings

settings = Settings.from_env()
embedder = make_embedder(settings)

for q in ["hl_mem", "唇形同步"]:
    result = embedder.embed(q)
    print(f"Query: {q}")
    print(f"  Type: {type(result)}")
    if result is not None:
        if isinstance(result, (list, tuple)):
            print(f"  Length: {len(result)}")
            print(f"  First 3: {result[:3]}")
            print(f"  Types of items: {set(type(x).__name__ for x in result[:10])}")
        elif isinstance(result, bytes):
            print(f"  Bytes length: {len(result)}")
        else:
            print(f"  Value: {str(result)[:200]}")
    else:
        print("  None!")
    print()
