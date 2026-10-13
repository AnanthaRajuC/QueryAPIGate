/** Why the server refused a sign-in through SSO (sso.py), in the Console's own words. The address carries only the
 * code: anyone can make a link to /console/?sso_error=..., so nothing on it is shown as it's written. */
const SSO_REFUSALS: Record<string, string> = {
  sso_unreachable:
    "the identity provider's sign-in can't be reached - try again shortly, or sign in with a token",
  sso_unsupported: "the identity provider doesn't support PKCE with S256",
  sso_busy: 'too many sign-ins are under way - try again in a few minutes',
  sso_state: 'this sign-in has expired or was started elsewhere - sign in again',
  sso_token: "the identity provider didn't accept this sign-in - sign in again",
  sso_id_token: "the identity provider's answer couldn't be verified - sign in again",
  sso_no_role:
    "you signed in, but you're in no group that's given a role here - ask an owner to add you to one",
  sso_inactive: 'your administrator account is switched off here - ask an owner',
  sso_denied: 'the identity provider ended the sign-in',
  sso_ambiguous: 'more than one administrator waits for an SSO sign-in with your email - ask an owner',
};

export function ssoRefusal(code: string): string {
  const known = Object.prototype.hasOwnProperty.call(SSO_REFUSALS, code) ? SSO_REFUSALS[code] : undefined;
  const shown = /^[a-z_]{1,40}$/.test(code) ? ` (${code})` : '';
  return known
    ? `Sign-in through SSO didn't go ahead: ${known}${shown}`
    : `Sign-in through SSO didn't go ahead${shown} - an owner can see why in the audit log`;
}
