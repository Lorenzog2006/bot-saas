#!/bin/bash
cd "$(dirname "$0")"
echo "Deploy BotSaaS in corso..."
vercel deploy --prod --yes
echo ""
echo "✓ Deploy completato!"
read -p "Premi Invio per chiudere..."
