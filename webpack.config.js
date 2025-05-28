const path = require('path');
const glob = require('glob');


// Dynamically find all .tsx and .ts files in static/js (excluding files like icons.tsx if desired)
const jsDir = path.join(__dirname, 'fighthealthinsurance', 'static', 'js');
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

module.exports = {
  // Set context to project root so modules resolve from root-level node_modules
  context: __dirname,
  mode: 'development',
  entry: entries,
  output: {
    path: path.resolve(__dirname, 'fighthealthinsurance/static/js/dist'),
    filename: '[name].bundle.js',
  },
  resolve: {
    // Ensure webpack looks in static/js/node_modules first
    modules: [
      path.resolve(__dirname, 'fighthealthinsurance', 'static', 'js', 'node_modules'),
      'node_modules'
    ],
    extensions: ['.tsx', '.ts', '.js'],
    alias: {
      '@sentry/browser': require.resolve('@sentry/browser'),
    },
  },
  module: {
    rules: [
      {
        test: /\.(ts|tsx)$/,
        use: {
          loader: 'ts-loader',
          options: {
            configFile: path.resolve(__dirname, 'fighthealthinsurance', 'static', 'js', 'tsconfig.json')
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
        use: ['style-loader', 'css-loader'],
      },
    ],
  },
  devtool: 'source-map',
};
