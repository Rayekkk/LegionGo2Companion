// Fake timers leave no native event-loop handle. A pending top-level promise
// therefore lets Node exit successfully unless completion is checked explicitly.
module.exports = function requireCompletion() {
  let completed = false;
  process.once('beforeExit', () => {
    if (!completed && !process.exitCode) {
      console.error('Frontend test did not complete: an awaited operation is still pending.');
      process.exitCode = 1;
    }
  });
  return () => { completed = true; };
};
