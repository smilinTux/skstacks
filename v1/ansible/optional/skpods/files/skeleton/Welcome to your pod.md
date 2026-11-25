# Welcome to your skpods.id pod

This is your personal data store. Apps you sign in to read and write here
only with your permission, and you can take it back at any time.

## Your identity

Your WebID is your login for every Solid app:

    https://<this server>/apps/solid/~<your account name>/profile/card#me

When an app asks "Sign in with Solid", give it this server's address. You
log in here, never in the app, and you choose what the app may touch.

## Where things live

Everything in the `solid` folder is your pod. The folders next to it are
plain storage that no Solid app can see.

| Folder | Who can see it |
|---|---|
| `solid/private/` | Only you. `documents`, `photos` and `notes` are ready for your own files. |
| `solid/public/` | Anyone with the link. Put things in `shared` that you want to publish. |
| `solid/inbox/` | Others can drop messages and invites here; only you can read them. |
| `solid/settings/` | Your profile settings and the index apps use to find your data. Leave these to the apps. |

Apps that store data for you create their own folders inside `solid/`,
for example `solid/skpods/v1/chat` for pod chat.

## Good to know

- Nothing in your pod is shared until you or an app you approved shares it.
- Revoking an app's access does not delete what it wrote: the data stays yours.
- You can delete this file. A new copy will not come back.

Learn more at https://skpods.id
