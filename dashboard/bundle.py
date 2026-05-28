"""Bundle the dashboard template + page_views.json into a single self-contained
HTML file the user can open with no server, no dependencies."""
from pathlib import Path
import json

HERE = Path(__file__).parent
TEMPLATE = HERE / "_template.html"
DATA = HERE / "page_views.json"
OUT = HERE / "PageUsageDashboard.html"


def main() -> None:
    template = TEMPLATE.read_text(encoding="utf-8")
    payload = DATA.read_text(encoding="utf-8")
    # Escape only the literal sequence "</script>" if it ever appeared in data
    # (it shouldn't, but defensive). The JSON itself is already safe.
    payload_safe = payload.replace("</script>", "<\\/script>")
    html = template.replace("__DATA__", payload_safe)
    OUT.write_text(html, encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
