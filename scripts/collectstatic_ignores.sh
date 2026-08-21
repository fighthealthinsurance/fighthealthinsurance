#!/bin/bash
# Shared collectstatic ignore list. Source this, then expand
# "${COLLECTSTATIC_IGNORES[@]}" into every ./manage.py collectstatic call.
#
# Why this exists: fighthealthinsurance/static/js is an app static dir
# (APP_STATIC_DIR, settings.py), so AppDirectoriesFinder collects EVERYTHING
# under it -- including node_modules, which is ~1.4GB. Django's default
# ignore_patterns are only ('CVS', '.*', '*~'), so the whole dependency tree
# was copied into STATIC_ROOT, ADDed into the image (k8s/Dockerfile) and
# served publicly by nginx at /static/js/node_modules/... alongside the
# TypeScript sources and build config. A dry run measured 59857 node_modules
# paths collected. node_modules is gitignored, which is exactly why this was
# easy to miss: absent from the repo, present at build time.
#
# Kept in one file rather than repeated at each call site because a new
# collectstatic invocation that forgets the flags is precisely how this
# regressed in the first place.
#
# NOT ignored on purpose -- do not "tidy" these in:
#
#   *.json broadly. state_help.json, microsites.json, blog_posts.json and
#   denial_language.json are read at runtime through
#   staticfiles_storage.open(). Only the npm/TS build config is named here.
#
#   *.md. views.py serves blog posts and FAQ entries by reading
#   staticfiles_storage.path(f"blog/{slug}.md") and f"faq/{slug}.md" out of
#   STATIC_ROOT at request time, so ignoring *.md 404s every blog and FAQ
#   page. The only .md it would have hidden is a developer notes file.
#
#   Anything under js/dist/. That is the webpack output (*.bundle.js, *.map,
#   *.wasm, *.mjs) and must keep shipping. It contains no *.ts, so the source
#   patterns below cannot strip the bundle. Note some vendor bundles have
#   "node_modules" inside their FILENAME (vendors-node_modules_canvg_...js);
#   --ignore matches path components, so they are unaffected -- and the nginx
#   deny rule is written as /node_modules/ with slashes for the same reason.
COLLECTSTATIC_IGNORES=(
  --ignore node_modules
  --ignore '*.ts'
  --ignore '*.tsx'
  --ignore 'package.json'
  --ignore 'package-lock.json'
  --ignore 'tsconfig*.json'
  --ignore 'webpack.config.js'
)
