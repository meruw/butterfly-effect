"""Builds docs/ (the published site) from web/butterfly-effect.html.

  python web/build.py

Writes docs/index.html as a standalone page, with the tags social networks use to
show a preview card when the link is shared, and docs/CNAME with the domain.
"""
import os

SITE = "https://chaos.meruw.dev"
TITLE = "Butterfly Effect"
DESCRIPTION = ("One wingbeat is enough to change tomorrow. 2,000 atmospheres of the Lorenz model, live: "
               "choose the initial difference, tap the butterfly and listen to the theme it plays.")
ICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
        "%3Crect width='32' height='32' rx='7' fill='%2305060d'/%3E"
        "%3Cpath d='M16 20C9 6 3 9 4 16s8 9 12 4c4 5 11 3 12-4S23 6 16 20Z' fill='none' stroke='%23eceaf2' stroke-width='1.8'/%3E"
        "%3Ccircle cx='16' cy='20' r='2' fill='%23ffce70'/%3E%3C/svg%3E")

here = os.path.dirname(os.path.abspath(__file__))
docs = os.path.normpath(os.path.join(here, "..", "docs"))
src = open(os.path.join(here, "butterfly-effect.html"), encoding="utf-8").read()

# The source is a fragment: title, links and styles first, then the body.
cut = src.index("</style>") + len("</style>")
head = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="description" content="{DESCRIPTION}">
<meta name="theme-color" content="#05060d">
<link rel="canonical" href="{SITE}/">
<link rel="icon" href="{ICON}">
<meta property="og:type" content="website">
<meta property="og:url" content="{SITE}/">
<meta property="og:title" content="{TITLE}">
<meta property="og:description" content="{DESCRIPTION}">
<meta property="og:image" content="{SITE}/og.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{TITLE}">
<meta name="twitter:description" content="{DESCRIPTION}">
<meta name="twitter:image" content="{SITE}/og.png">
<style>body{{margin:0}}[hidden]{{display:none!important}}</style>
{src[:cut].strip()}
</head>
<body>
"""
os.makedirs(docs, exist_ok=True)
with open(os.path.join(docs, "index.html"), "w", encoding="utf-8", newline="\n") as f:
    f.write(head + src[cut:].strip() + "\n</body>\n</html>\n")
with open(os.path.join(docs, "CNAME"), "w", encoding="utf-8", newline="\n") as f:
    f.write(SITE.split("//")[1] + "\n")
open(os.path.join(docs, ".nojekyll"), "w").close()
print("wrote", docs)
