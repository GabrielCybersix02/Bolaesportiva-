import slots
for k, g in slots.GAMES.items(): print(k, "RTP exato: %.2f%%" % (slots.rtp(g) * 100))
