+++
title = "blyg"
outputs = ["blygmanifest", "blygfeed"]

# Items get no HTML page of their own, only the blyg surfaces; scoped to
# `kind = "page"` so this section page, which builds those surfaces,
# still renders.
[[cascade]]
  [cascade.target]
    kind = "page"
  [cascade.build]
    render = "never"
    list = "always"
+++
