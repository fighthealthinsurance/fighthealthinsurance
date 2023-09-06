const path = require('path');

module.exports = {
  entry: {
    'shared': './shared.ts',
    'scrub': './scrub.ts',
    'appeal': './appeal.ts',  
  },
  output: {
    filename: '[name].bundle.js',
    path: path.resolve(__dirname, 'dist'),
  },
  experiments: {
    topLevelAwait: true
  },
};
