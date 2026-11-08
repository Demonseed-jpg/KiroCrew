"use strict";
// The release channel the desktop update lane must follow, or "" when the
// Settings preference decides.
//
// An enterprise build follows `lts`, the edition's own channel (RFC
// crew-enterprise-edition, D10 and appendix row "Update channel"), the same
// channel the backend's update paths follow (kiro_crew/platform/update_layout.py
// policy_release_channel). `lts` is not a channel an oss build offers, so neither
// edition reads the other's feed. The Settings preference cannot widen it, so it
// replaces the preference (auto-update.js resolveChannel) and the switcher
// refuses (update:set-channel). An oss build has no required channel: nothing
// here changes it.
const { EDITION_ENTERPRISE, readBakedEdition } = require("../../edition");

const ENTERPRISE_UPDATE_CHANNEL = "lts";

/**
 * @param {"oss"|"enterprise"} [edition] the baked edition (edition.js)
 * @returns {() => ""|"lts"}
 */
function requiredUpdateChannelFor(edition = readBakedEdition()) {
  const required = edition === EDITION_ENTERPRISE ? ENTERPRISE_UPDATE_CHANNEL : "";
  return () => required;
}

module.exports = { ENTERPRISE_UPDATE_CHANNEL, requiredUpdateChannelFor };
