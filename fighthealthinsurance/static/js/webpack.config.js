const path = require('path');
const fs = require('fs');
const glob = require('glob');
const TerserPlugin = require('terser-webpack-plugin');
const CopyPlugin = require('copy-webpack-plugin');
const { BundleAnalyzerPlugin } = require('webpack-bundle-analyzer');

// Runtime worker assets that pdf.js and tesseract.js load by URL at runtime
// (they cannot be bundled). They used to be served straight out of
// /static/js/node_modules/, which stopped being published (collectstatic
// ignore + nginx deny, PR #936) -- so they are copied into dist/workers/,
// which ships with the bundles. Nothing at runtime may reference a
// node_modules URL; tests/async-unit/test_worker_assets.py pins that.
const workerAssets = [
  // Published as .js on purpose. nginx in the web image (Debian bookworm's
  // mime.types) has no entry for .mjs, so the worker went out as
  // application/octet-stream, and browsers refuse to run a module worker or
  // dynamic import() without a JavaScript MIME type. pdf.js then fell back to
  // its "fake worker", which does the same import and failed the same way, so
  // every PDF a user attached on /scan was unreadable ("Setting up fake worker
  // failed"). Same bytes; only the extension decides the MIME type.
  // tests/async-unit/test_worker_assets.py pins the extension.
  { from: 'node_modules/pdfjs-dist/build/pdf.worker.min.mjs', to: 'workers/pdf.worker.min.js' },
  { from: 'node_modules/tesseract.js/dist/worker.min.js', to: 'workers/tesseract.js/worker.min.js' },
  { from: 'node_modules/tesseract.js-core/*.{js,wasm}', to: 'workers/tesseract.js-core/[name][ext]' },
];

// Check if bundle analysis is requested
const shouldAnalyze = process.env.ANALYZE === 'true';

// Dynamically find all .tsx and .ts files in static/js (excluding files like icons.tsx if desired)
const jsDir = path.join(__dirname);
const entries = {};
try {
  glob.sync(path.join(jsDir, '*.{ts,tsx}')).forEach(file => {
    const name = path.basename(file).replace(/\.(tsx|ts)$/, '');
    // Exclude utility files and test files from being entry points
    if (!['icons', 'utils', 'types'].includes(name) && !name.includes('.test') && !name.includes('.spec')) {
      entries[name] = file;
    }
  });
} catch (error) {
  console.error('Error scanning for entry points:', error);
  process.exit(1);
}

// Determine if we're in production mode
// Production unless a developer explicitly asks for a development build.
//
// This used to test for NODE_ENV === 'production', and nothing in the repo
// ever set it: not `npm run build`, not scripts/ci_npm_build.sh,
// build_static.sh or setup_templates.sh, not the CI workflow. So every build,
// including the one collected into the deployed image, was a development
// bundle: unminified, about 3.5x the size, and -- the part that matters --
// skipping the optimization block below, whose Terser pure_funcs strip
// console.log/info/debug as a defense against logging PHI to the browser
// console. Defaulting the other way means the safe build is the one you get
// by accident. `npm run build:dev` (or NODE_ENV=development) opts out.
const isProduction = process.env.NODE_ENV !== 'development';

module.exports = async (env, argv) => {
  // Load ESM-only plugins with dynamic import()
  const [{ default: remarkGfm }, { default: rehypeHighlight }] = await Promise.all([
    import('remark-gfm'),
    import('rehype-highlight'),
  ]);
  return {
  context: __dirname,
  mode: isProduction ? 'production' : 'development',
  entry: entries,
  output: {
    path: path.resolve(__dirname, 'dist'),
    filename: '[name].bundle.js',
  },
  // Production optimizations
  optimization: isProduction ? {
    minimize: true,
    minimizer: [
      new TerserPlugin({
        terserOptions: {
          compress: {
            // Strip chatty console levels from production bundles as a
            // defense-in-depth against logging user health data (PHI) to the
            // browser console. console.warn/error are kept so production
            // issues stay debuggable (no browser error reporter is
            // initialized) -- never log PHI at those levels.
            pure_funcs: ['console.log', 'console.info', 'console.debug'],
          },
          format: {
            comments: false,
          },
        },
        extractComments: false,
      }),
    ],
  } : {},
  resolve: {
    modules: [
      path.resolve(__dirname, 'node_modules'),
      'node_modules'
    ],
  extensions: ['.tsx', '.ts', '.js', '.md'],
    alias: {
      '@sentry/browser': require.resolve('@sentry/browser'),
    },
  },
  module: {
    rules: [
      {
        test: /\.md?$/,
        use: [
          {
            loader: 'babel-loader',
            options: {
              presets: ['@babel/preset-react']
            }
          },
          {
            loader: '@mdx-js/loader',
            options: {
              remarkPlugins: [remarkGfm],
              rehypePlugins: [rehypeHighlight]
            }
          }
        ]
      },
      {
        test: /\.(ts|tsx)$/,
        use: {
          loader: 'ts-loader',
          options: {
            configFile: path.resolve(__dirname, 'tsconfig.json')
          }
        },
        exclude: /node_modules/,
      },
      {
        test: /\.(js|jsx)$/,
        use: 'babel-loader',
        exclude: /node_modules/,
      },
      {
        test: /\.css$/,
        exclude: /\.module\.css$/,
        use: ['style-loader', 'css-loader'],
      },
      {
        test: /\.module\.css$/,
        use: [
          'style-loader',
          {
            loader: 'css-loader',
            options: {
              modules: {
                localIdentName: '[name]__[local]--[hash:base64:5]',
              },
              importLoaders: 1,
            },
          },
        ],
      },
    ],
  },
  // Generate source maps in both development and production (OSS project, helpful for debugging)
  devtool: 'source-map',
  // Plugins - the worker asset copy always; the bundle analyzer on request
  plugins: [
    new CopyPlugin({ patterns: workerAssets }),
    // Record which mode produced dist/, so scripts/build_static.sh can tell a
    // production build from a development one. Its cache keys on source
    // checksums only, so without this an `npm run build:dev` in between would
    // leave development output in place under an unchanged checksum and the
    // next build_static.sh would skip webpack and collect it (review).
    {
      apply(compiler) {
        const marker = path.resolve(__dirname, 'dist', 'BUILD_MODE');
        // Invalidate first, write last. An interrupted or failed build must
        // not leave the previous mode's marker next to partially overwritten
        // output, or build_static.sh would trust it and skip the rebuild
        // (review). So the marker is removed before every compilation and
        // written back only after an error-free emit.
        compiler.hooks.beforeCompile.tap('BuildModeMarker', () => {
          try {
            fs.unlinkSync(marker);
          } catch (e) {
            if (e.code !== 'ENOENT') throw e;
          }
        });
        compiler.hooks.afterEmit.tap('BuildModeMarker', (compilation) => {
          if (compilation.errors.length > 0) return;
          // The EFFECTIVE mode, not isProduction: `webpack --mode development`
          // overrides the configured mode after this file has run, and the
          // marker has to describe what was actually built (review).
          const mode = compiler.options.mode === 'production' ? 'production' : 'development';
          fs.writeFileSync(marker, mode + '\n');
        });
        // Not covered, on purpose: two webpack processes writing this dist/ at
        // once. They corrupt the bundles themselves, marker or not, so
        // concurrent builds in one working tree are unsupported, and
        // build_static.sh runs one build at a time.
      },
    },
    ...(shouldAnalyze ? [
      new BundleAnalyzerPlugin({
        analyzerMode: 'static',
        reportFilename: 'bundle-report.html',
        openAnalyzer: true,
      }),
    ] : []),
  ],
};
}
