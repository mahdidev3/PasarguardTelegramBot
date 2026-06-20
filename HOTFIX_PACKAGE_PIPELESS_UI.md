# Hotfix: Package admin UI without pipe-separated input

This patch removes the bad `|`-separated admin input flow from the package feature.

## Changed

- Manual package item creation is now step-by-step:
  1. title
  2. data amount
  3. duration days
  4. sub price
  5. preview with edit buttons
  6. save item

- Sales-plan package item creation is now step-by-step:
  1. select sales plan
  2. enter/use sub price
  3. preview with edit buttons
  4. optionally edit title/data/days/price with separate buttons
  5. save item

- Per-user package customization is now button-based:
  - edit package price
  - edit max subscriptions
  - edit description
  - edit conditions
  - edit assignment code

## Removed from package UX

- No `title|data|days|price` input for manual package items.
- No `price|title|data|days` input for sales-plan override.
- No `price|max_subs|description|conditions|code` input for per-user override.

## Validation

Each field is validated independently and returns to the preview after saving.

## Check

`python3 -m compileall -q app main.py` passed.
