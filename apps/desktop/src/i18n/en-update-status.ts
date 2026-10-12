export const backendUpdateStatusMessages = {
  preparing: 'Updating backend…',
  pulling: 'Backend updating…',
  restarting: 'Backend restarting to load the update…',
  notAvailable: 'Update not available for this backend.',
  failed: 'Backend update failed.',
  completionUnknown: 'Completion could not be confirmed. The update may still be running. Close this window and check its status before retrying.',
  noReturn: 'Backend didn’t come back online. The update may not have completed — check the backend host.',
  owed: (steps: string) => `Backend updated, but still owed: ${steps}. Re-run \`hermes update\` to finish them.`
}
