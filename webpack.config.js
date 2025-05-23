
const path = require('path');
const glob = require('glob');


// Dynamically find all .tsx and .ts files in static/js (excluding files like icons.tsx if desired)
const jsDir = path.join(__dirname, 'fighthealthinsurance', 'static', 'js');
const entries = {};
glob.sync(path.join(jsDir, '*.{ts,tsx}')).forEach(file => {
  const name = path.basename(file).replace(/\.(tsx|ts)$/, '');
  // Exclude utility files from being entry points if needed
  if (!['icons'].includes(name)) {
    entries[name] = file;
  }
});

module.exports = {
  mode: 'development',
  entry: entries,
  output: {
    path: path.resolve(__dirname, 'fighthealthinsurance/static/js/dist'),
    filename: '[name].bundle.js',
  },
  resolve: {
    extensions: ['.tsx', '.ts', '.js'],
  },
  module: {
    rules: [
      {
        test: /\.(ts|tsx)$/,
        use: 'ts-loader',
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
