#!/bin/sh
# Defaults; override in /etc/lesswrong-bot.env on the server (see docs/deploy.md).
IRC_NICK=${IRC_NICK:-sbot} exec python3 bot.py
