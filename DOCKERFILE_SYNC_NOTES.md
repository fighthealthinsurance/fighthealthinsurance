# Dockerfile Synchronization Notes

## Important: Keep Dockerfiles in Sync with CI Configuration

This project has multiple Dockerfiles that may need to be updated when CI configuration changes:

### File Locations:
- **CI Configuration**: `.github/workflows/ci.yml`
- **Main Dockerfile**: `k8s/Dockerfile`  
- **Ray Combined Dockerfile**: `k8s/ray/CombinedDockerfile`

### When to Review and Update:

#### If changes are made to `.github/workflows/ci.yml`, review these Dockerfiles:

1. **`k8s/Dockerfile`** - Used for main application deployments
2. **`k8s/ray/CombinedDockerfile`** - Used for staging/ray-based deployments

#### Common Changes to Watch For:

1. **System Package Dependencies**
   - If CI installs new `apt` packages for build dependencies
   - Example: Adding cairo libraries (`libcairo2-dev`, `libgirepository1.0-dev`, etc.)
   
2. **Python Build Requirements**
   - New build tools or development headers
   - Example: `python3-dev`, `pkg-config`, `build-essential`
   
3. **External Tools**
   - New tools needed for testing or building
   - Example: `pandoc`, `texlive`, `wkhtmltopdf`

4. **Build Steps and Commands**
   - Django management commands like blog metadata generation
   - File copying operations that affect runtime functionality
   - Working directory changes and file permissions

#### Recent Example (September 2025):
When pycairo build issues were fixed in CI by adding cairo development libraries, the same packages needed to be added to both Dockerfiles:
- Added to `k8s/Dockerfile`: `libcairo2-dev libgirepository1.0-dev pkg-config`  
- Added to `k8s/ray/CombinedDockerfile`: `libcairo2-dev libgirepository1.0-dev` (pkg-config was already present)

Another issue: The blog wasn't loading on staging because the CombinedDockerfile was missing the blog metadata generation step:
- Added to `k8s/ray/CombinedDockerfile`: Blog metadata generation command to create `blog_posts.json`

### Action Items When CI Changes:
1. Review the CI changes for new system dependencies
2. Check if those dependencies are needed in production/staging
3. Update both Dockerfiles with the same dependencies
4. Maintain consistent package ordering for build optimization
5. Test both Docker builds to ensure they work

### Package Installation Order for pycairo:
When installing cairo-related packages, maintain this order:
```bash
build-essential libcairo2-dev libgirepository1.0-dev pkg-config python3-dev
```

This ensures cairo libraries are available before pkg-config tries to find them and python3-dev provides headers for C extension compilation.