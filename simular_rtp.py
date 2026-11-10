"""Confere o RTP dos jogos. Rodar:  python simular_rtp.py"""
import slots, random
print("Fruity Jackpot - RTP exato: %.2f%%" % (slots.rtp_slot() * 100))
n = 300000; tot = 0; hit = 0
for _ in range(n):
    w = slots.evaluate(slots.spin_grid(), 1000)[0]; tot += w; hit += w > 0
print("  simulado (%d giros): RTP %.2f%% | acerto %.1f%%" % (n, tot / n / 10, hit / n * 100))
for alvo in (1.5, 2, 5, 10):                       # Aviator: qualquer alvo de saque devolve ~RTP
    g = sum(1 for _ in range(n) if slots.sortear_crash() >= alvo * 100)
    print("Aviator saque %.1fx: acerta %.1f%% -> RTP %.2f%%" % (alvo, g / n * 100, g / n * alvo * 100))
print("Aviator: RTP configurado %.1f%% (lucro da casa %.1f%%)" % (slots.AV_RTP * 100, 100 - slots.AV_RTP * 100))
