#!/usr/bin/env node
// `busymeter level 5 --for 10m`, `busymeter clear`, `busymeter status`. Ctrl+C clears a level that is being held.
import { main } from '../lib/cli.js';

const controller = new AbortController();
const onSigint = () => {
  if (controller.signal.aborted) process.exit(130); // a second Ctrl+C: don't wait for the clear
  controller.abort();
};
process.on('SIGINT', onSigint);
try {
  process.exitCode = await main(process.argv.slice(2), { signal: controller.signal });
} finally {
  process.off('SIGINT', onSigint);
}
