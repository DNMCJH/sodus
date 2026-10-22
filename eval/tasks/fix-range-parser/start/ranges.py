"""Parse compact range strings like "1-3,5,8-10" into a sorted list of ints."""


def parse_ranges(text: str) -> list[int]:
    out: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-")
            out.extend(range(int(lo), int(hi)))
        else:
            out.append(int(part))
    return sorted(set(out))
