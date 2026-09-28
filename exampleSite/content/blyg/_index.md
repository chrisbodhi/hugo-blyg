+++
title = "blyg"
outputs = ["html", "blygmanifest", "blygfeed"]   # "html": the feed page, at /blyg/

# Hugo renders no page of its own for an item: hugo-blyg writes each
# item's HTML at its id-based permalink (f/{id}/, t/{id}/) itself, next
# to the JSON surfaces. Scoped to `kind = "page"` so this section page,
# which builds all of that, still renders.
[[cascade]]
  [cascade.target]
    kind = "page"
  [cascade.build]
    render = "never"
    list = "always"
+++
