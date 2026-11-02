# Loop design rules

## Chats open as full pages

Conversation threads in Messages always open as a full view/page
(`/messages/<username>`), with a back link to the Messages list.
Never open a chat as a bottom sheet or bottom-opening drawer —
bottom-open chats are confusing.

This applies to every chat surface in the app: the conversation list
links to full thread pages, and threads never slide up from the bottom
of the screen.
