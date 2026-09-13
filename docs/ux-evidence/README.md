# UX evidence

Before-and-after screenshots for pull requests that change what a page looks
like. A reviewer reading a CSS diff cannot tell whether a button got better or
just different, and neither can the person who has to approve the change.

These are review material, not site assets:

- Nothing here is served. The directory sits outside `fighthealthinsurance/`,
  so `collectstatic` never sees it and no template can reference it.
- `.gitattributes` marks the directory `-diff linguist-generated=true`, so a
  pull request collapses it rather than pasting image blobs into the code
  diff. The files stay one click away.
- Keep them small. Crop to the thing that changed and stay under a few hundred
  kilobytes each; a full-page capture at retina width helps nobody and lives
  in the repository forever.

Name a file for the pull request and the state it shows:

    pr6-buttons-home-before.png
    pr6-buttons-home-after.png

Old evidence can be deleted once the pull request it belonged to is merged and
the question it answered is settled.
