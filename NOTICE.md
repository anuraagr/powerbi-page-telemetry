# Third-party notices

This reference implementation includes or links to the following
third-party components. Their licenses and notices are reproduced
below as required.

## Chart.js (bundled in dashboard/PageUsageDashboard.html)

The static dashboard loads Chart.js from the official CDN to render
its visualizations:

```html
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
```

Chart.js is distributed under the MIT License.

```
The MIT License (MIT)

Copyright (c) 2014-present Chart.js Contributors

Permission is hereby granted, free of charge, to any person obtaining
a copy of this software and associated documentation files (the
"Software"), to deal in the Software without restriction, including
without limitation the rights to use, copy, modify, merge, publish,
distribute, sublicense, and/or sell copies of the Software, and to
permit persons to whom the Software is furnished to do so, subject to
the following conditions:

The above copyright notice and this permission notice shall be
included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
```

Upstream: <https://github.com/chartjs/Chart.js>.

## Python runtime dependencies (etl/requirements.txt)

The collector depends on the following Python packages. All are
permissively licensed; see each project for full text.

| Package | Used for | License | Upstream |
|---|---|---|---|
| `requests` | Power BI REST and AAD token calls | Apache-2.0 | <https://github.com/psf/requests> |
| `azure-identity` (Functions only) | Managed Identity for Key Vault and Storage | MIT | <https://github.com/Azure/azure-sdk-for-python> |
| `azure-storage-blob` (Functions only) | Writing silver CSV to ADLS / Blob | MIT | <https://github.com/Azure/azure-sdk-for-python> |
| `azure-functions` (Functions only) | Function trigger bindings | MIT | <https://github.com/Azure/azure-functions-python-library> |

`pyadomd` (optional) is **not** distributed with the reference
implementation. It is loaded at runtime only when `PBI_USE_PYADOMD=1`
and the customer has installed it separately along with the
ADOMD.NET retail client (a Microsoft component licensed separately
under the Microsoft Software License Terms).

## Development tooling

These are only used in development / CI, not at runtime:

| Tool | Purpose | License |
|---|---|---|
| `pytest` | Test runner | MIT |
| `ruff` | Linter / formatter | MIT |
| `Pillow` (one-off) | Architecture diagram PNG re-encoding | HPND (BSD-style) |

## Diagram source

`architecture.excalidraw` is the editable source for `architecture.png`.
Excalidraw is MIT-licensed; see <https://github.com/excalidraw/excalidraw>.
