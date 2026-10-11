"""Read bounded diagnostics from an update receipt, never an arbitrary client path."""
from pathlib import Path


def diagnostic_details(home: Path, receipt: dict) -> str:
    root = (home / "logs" / "maintained-tests").resolve()
    sections = [f"Update: {receipt.get('update_id', 'unknown')}\nOutcome: {receipt.get('outcome', 'unknown')}"]
    paths = []
    for item in receipt.get("followups") or []:
        reason = str(item.get("reason") or "")
        sections.append(f"{item.get('step')}: {reason}")
        if "diagnostics: " in reason:
            paths.append(reason.partition("diagnostics: ")[2])
    repair = receipt.get("managed_repair") or {}
    if repair:
        sections.append(f"Automatic repair: {repair.get('status')}\n{repair.get('reason') or repair.get('summary') or ''}")
        if repair.get("log"):
            paths.append(repair["log"])
    for name in dict.fromkeys(paths):
        path = Path(name)
        try:
            # Only direct regular logs produced by this owner; reject symlinks,
            # traversal, and all other home files even if a receipt is malformed.
            if path.is_symlink() or path.resolve().parent != root or not path.is_file():
                continue
            with path.open("rb") as handle:
                handle.seek(max(0, path.stat().st_size - 128 * 1024))
                text = handle.read(128 * 1024).decode("utf-8", "replace")
            sections.append(f"--- {path.name} ---\n{text}")
        except OSError:
            continue
    return "\n\n".join(sections)
