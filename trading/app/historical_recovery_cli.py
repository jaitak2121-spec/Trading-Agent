"""Resume missing CoinSwitch candles into a separate, checkpointed artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from ..adapters.coinswitch.historical_candles import CoinSwitchCandleTransport
from ..adapters.observation_evidence import write_json_atomically
from ..core.secrets import Secret
from .historical_candles import recover_historical_candles


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Recover genuine missing CoinSwitch candles")
    parser.add_argument("original", help="immutable original candle artifact")
    parser.add_argument("output", help="separate recovered/checkpoint artifact")
    parser.add_argument("--chunk-candles", type=int, default=1440)
    parser.add_argument("--confirmations", type=int, default=2)
    args = parser.parse_args(argv)

    original = Path(args.original).resolve()
    output = Path(args.output).resolve()
    if original == output:
        parser.error("recovery output must not overwrite the original dataset")
    original_bytes = original.read_bytes()
    original_hash = hashlib.sha256(original_bytes).hexdigest()
    source_path = output if output.exists() else original
    source_bytes = source_path.read_bytes()
    dataset = json.loads(source_bytes, parse_float=str)
    if output.exists():
        recorded = dataset.get("source", {}).get("recovery", {}).get("original_file_sha256")
        if recorded != original_hash:
            parser.error("existing recovery checkpoint belongs to a different original dataset")

    api_key = os.environ.get("COINSWITCH_API_KEY")
    api_secret = os.environ.get("COINSWITCH_API_SECRET")
    if not api_key or not api_secret:
        parser.error("recovery requires CoinSwitch credentials in process environment")
    transport = CoinSwitchCandleTransport(
        api_key=Secret(api_key, label="coinswitch_api_key"),
        api_secret=Secret(api_secret, label="coinswitch_api_secret"),
    )
    recovered, report = recover_historical_candles(
        dataset,
        fetch=transport.fetch_with_details,
        input_file_sha256=hashlib.sha256(source_bytes).hexdigest(),
        chunk_candles=args.chunk_candles,
        confirmations=args.confirmations,
        checkpoint=lambda value: write_json_atomically(output, value),
    )
    classification = recovered["source"]["recovery"]["timestamp_classification"]
    print(report.render())
    print(f"original sha256   : {original_hash}")
    print(f"recovered dataset : {output}")
    print(f"classification    : {classification['counts']}")
    return 0 if report.passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
