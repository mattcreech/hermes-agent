"""Read bounded diagnostics from an update receipt, never an arbitrary client path."""
from pathlib import Path


def diagnostic_details(home: Path, receipt: dict) -> str:
    root = (home / "logs" / "maintained-tests").resolve()
    preparation_root = (home / "logs" / "prepared-updates").resolve()
    sections = [f"Update: {receipt.get('update_id', 'unknown')}\nOutcome: {receipt.get('outcome', 'unknown')}"]
    paths = []
    for item in receipt.get("followups") or []:
        reason = str(item.get("reason") or "")
        sections.append(f"{item.get('step')}: {reason}")
        if "diagnostics: " in reason:
            paths.append((reason.partition("diagnostics: ")[2], root))
    repair = receipt.get("managed_repair") or {}
    if repair:
        sections.append(f"Automatic repair: {repair.get('status')}\n{repair.get('reason') or repair.get('summary') or ''}")
        if repair.get("log"):
            paths.append((repair["log"], root))
    preparation = receipt.get("preparation_result")
    if isinstance(preparation, dict):
        lines = ["Update preparation"]
        for key, label in (("status", "Status"), ("stage", "Stage"), ("reason", "Reason"),
                           ("requested_upstream_sha", "Requested upstream")):
            value = preparation.get(key)
            if isinstance(value, str) and value:
                lines.append(f"{label}: {value[:4096]}")
        decision = preparation.get("repair_decision")
        if isinstance(decision, dict):
            for key in ("reason", "summary"):
                value = decision.get(key)
                if isinstance(value, str) and value:
                    lines.append(f"Repair {key}: {value[:4096]}")
        sections.append("\n".join(lines))
        if isinstance(preparation.get("log"), str):
            # This is the local request log recorded by the native caller.
            # Coordinator details and proposed remote log paths are data only.
            paths.append((preparation["log"], preparation_root))
    for name, allowed_root in dict.fromkeys(paths):
        path = Path(name)
        try:
            # Only direct regular logs produced by this owner; reject symlinks,
            # traversal, and all other home files even if a receipt is malformed.
            if (not path.is_absolute() or ".." in path.parts or path.is_symlink()
                    or path.resolve().parent != allowed_root or not path.is_file()):
                continue
            with path.open("rb") as handle:
                handle.seek(max(0, path.stat().st_size - 128 * 1024))
                text = handle.read(128 * 1024).decode("utf-8", "replace")
            sections.append(f"--- {path.name} ---\n{text}")
        except OSError:
            continue
    return "\n\n".join(sections)
