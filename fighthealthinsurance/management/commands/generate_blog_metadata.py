import os
import re
import yaml
import json
from django.core.management.base import BaseCommand
from django.conf import settings

class Command(BaseCommand):
    help = 'Scan static/blog/ for .mdx files and extract frontmatter metadata to blog_posts.json.'

    FRONTMATTER_RE = re.compile(r'^---\s*([\s\S]+?)\s*---', re.MULTILINE)

    def handle(self, *args, **options):
        static_dir = os.path.join(settings.BASE_DIR, 'fighthealthinsurance', 'static', 'blog')
        output_path = os.path.join(settings.BASE_DIR, 'fighthealthinsurance', 'static', 'blog_posts.json')
        if not os.path.exists(static_dir):
            self.stderr.write(self.style.ERROR(f"Blog directory does not exist: {static_dir}"))
            return
        if not os.path.isdir(static_dir):
            self.stderr.write(self.style.ERROR(f"Blog path is not a directory: {static_dir}"))
            return
        if not os.access(static_dir, os.R_OK):
            self.stderr.write(self.style.ERROR(f"Blog directory is not readable: {static_dir}"))
            return
        posts = []
        try:
            for fname in os.listdir(static_dir):
                if fname.endswith('.mdx'):
                    path = os.path.join(static_dir, fname)
                    try:
                        with open(path, 'r', encoding='utf-8') as f:
                            content = f.read()
                    except Exception as e:
                        self.stderr.write(self.style.ERROR(f"Failed to read {path}: {e}"))
                        continue
                    m = self.FRONTMATTER_RE.match(content)
                    if m:
                        try:
                            frontmatter = yaml.safe_load(m.group(1))
                        except Exception as e:
                            self.stderr.write(self.style.ERROR(f"Failed to parse frontmatter in {fname}: {e}"))
                            continue
                        slug = os.path.splitext(fname)[0]
                        post = {'slug': slug}
                        if isinstance(frontmatter, dict):
                            post.update(frontmatter)
                        posts.append(post)
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Error listing files in {static_dir}: {e}"))
            return
        try:
            with open(output_path, 'w', encoding='utf-8') as out:
                json.dump(posts, out, indent=2)
            self.stdout.write(self.style.SUCCESS(f'Wrote metadata for {len(posts)} posts to {output_path}'))
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Failed to write output file {output_path}: {e}"))
