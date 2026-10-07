#!/bin/bash
# Ensure markitdown (with all file-format extras) is available in every session.
set -uo pipefail
if ! python3 -c "import markitdown, pdfminer, mammoth, pptx, openpyxl" >/dev/null 2>&1; then
  pip install --quiet 'markitdown[all]' >/dev/null 2>&1 || echo "markitdown install failed" >&2
fi
exit 0
