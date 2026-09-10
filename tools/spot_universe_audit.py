from __future__ import annotations

from oitgbot.clients.binance_api import BinanceAPI
from oitgbot.services.spot_backed_universe import SpotBackedUniverse


def main() -> None:
    api = BinanceAPI()
    try:
        result = SpotBackedUniverse().refresh(api)
    finally:
        api.close()

    print(f"futures_eligible={result.futures_eligible}")
    print(f"active_spot_usdt={result.active_spot_usdt}")
    print(f"kept_total={len(result.kept)}")
    print(f"exact={result.count('exact')}")
    print(f"multiplier={result.count('multiplier')}")
    print(f"alias={result.count('alias')}")
    print(f"excluded_unresolved={len(result.excluded)}")
    for item in result.kept:
        if item.match_type != "exact":
            print(
                f"KEEP  {item.futures_symbol:<18} -> "
                f"{item.spot_symbol:<18} {item.match_type}"
            )
    for item in result.excluded:
        print(f"DROP  {item.futures_symbol:<18} -> {'-':<18} {item.reason}")


if __name__ == "__main__":
    main()
