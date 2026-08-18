# Company-facing platform overview

Materials for sharing the expense tracker project internally (architecture review, demo prep, engineering showcase).

| File | Purpose |
|------|---------|
| `Expense_Tracker_Platform_Overview.docx` | High-level Word document (executive summary, layers, security, deployment, stack) |
| `architecture-diagram.png` | Slide-ready architecture figure (also embedded in the Word doc) |
| `architecture-diagram.mmd` | Editable Mermaid source — update diagram here, re-export PNG if needed |

## Regenerate the Word document

```bash
pip install python-docx
python scripts/build_company_overview.py
```

Place `architecture-diagram.png` in this folder before rebuilding if you replace the figure.

## Export diagram only

1. Open [mermaid.live](https://mermaid.live)
2. Paste `architecture-diagram.mmd`
3. Export PNG/SVG for PowerPoint or Confluence
