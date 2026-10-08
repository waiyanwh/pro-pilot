# Slack App

## Manifest

Create the app from this manifest: <https://api.slack.com/apps> → **Create New App** → **From a manifest**.

```yaml
display_information:
  name: pro-pilot
  description: Read-only answers about demo Kubernetes clusters
  background_color: "#1f2937"
features:
  bot_user:
    display_name: pro-pilot
    always_online: true
oauth_config:
  scopes:
    bot:
      - app_mentions:read   # receive @pro-pilot mentions
      - chat:write          # post/update answers and ephemeral refusals
      - reactions:read      # 👍/👎 feedback on answers
settings:
  event_subscriptions:
    bot_events:
      - app_mention
      - reaction_added
  interactivity:
    is_enabled: false
  org_deploy_enabled: false
  socket_mode_enabled: true
  token_rotation_enabled: false
```

Scopes deliberately **not** requested: `channels:history`, `groups:history`, `im:history`, `im:read`, `users:read`, `files:*`, `chat:write.public`. The bot only sees messages that mention it and only posts where it is invited.

## Personal workspace setup (Phase 0–1)

1. Create the app from the manifest in your personal workspace.
2. **Basic Information → App-Level Tokens → Generate**: scope `connections:write`, name `socket`. Copy the `xapp-…` token.
3. **Install App → Install to Workspace**. Copy the bot token `xoxb-…`.
4. Create a channel, e.g. `#pro-pilot-test`, and `/invite @pro-pilot`.
5. Get the channel ID (channel name → **View channel details** → bottom of the About tab) and add it to `config/hub.local.yaml` under `slack.channels`.
6. Create a second channel that is **not** in the config. You will use it to test the refusal path.
7. Put the tokens in `.env` (git-ignored):

   ```bash
   PRO_PILOT_SLACK_BOT_TOKEN=xoxb-...
   PRO_PILOT_SLACK_APP_TOKEN=xapp-...
   PRO_PILOT_SPOKE_TOKENS_JSON={"demo-cluster":["sha256:<hex of dev token>"]}
   PRO_PILOT_SPOKE_TOKEN=<dev token>          # read by the local spoke
   ```

   Generate the dev token with `python -c "import secrets;print(secrets.token_urlsafe(32))"` and its hash with `python -c "import hashlib,sys;print(hashlib.sha256(sys.argv[1].encode()).hexdigest())" <token>`.

## Company workspace (Phase 3)

- Recreate the app from the same manifest in the company workspace. Do not reuse personal-workspace tokens.
- The workspace admin approves the app. Note in the request that it uses Socket Mode, requests the three scopes above, and does not read channel history.
- Tokens are stored as a SOPS-encrypted Secret in Pro Infra production.

## UX conventions

| Situation | Bot behaviour |
| --- | --- |
| Mention in an allowed channel | Thread reply placeholder `:mag: Looking into it…`, replaced by the answer |
| Mention in a non-allowed channel | Ephemeral: "pro-pilot isn't enabled in this channel. Ask your platform team to add it." |
| Rate limited | Ephemeral: "You've hit the limit (10 questions / 10 min). Try again in N min." |
| Empty mention | Thread reply with 3 plain-language examples about environment health, application restarts and deployment status |
| Follow-up in the same thread | Mention again in the thread; prior context is reused (ADR-011) |
| Answer layout | Short status headline, plain-language answer, optional `What I found` / `What to do next`, then a divider and feedback |
| Answer footer | Context block: `Pro Infra Staging · 👍 Helpful · 👎 Needs improvement`; feedback only when no tool has a cluster |
