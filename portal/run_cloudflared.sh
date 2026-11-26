#!/bin/sh

token_file=/home/pi/.config/campus-ems/cloudflared-token
if [ ! -s "$token_file" ]; then
    echo "Cloudflare Tunnel token is not configured"
    exit 0
fi

exec /usr/local/bin/cloudflared tunnel --no-autoupdate run --token-file "$token_file"
