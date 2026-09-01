# 🛰️ Signal2 — Veille IA autonome 24/7 (MASSRACE)

Veille active sur **toutes les sorties de modèles d'IA** (Anthropic, OpenAI,
Google DeepMind, Meta, DeepSeek, Qwen/Alibaba, Z.ai/Zhipu, Moonshot/Kimi,
Mistral, xAI, Black Forest Labs, NVIDIA, Microsoft + tout nouvel acteur majeur),
publiée automatiquement sur le salon **#llm** du serveur Discord **MASSRACE**.

## ⚙️ Infrastructure — 100 % gratuite et indépendante de tout PC

- **Hébergement** : GitHub Actions (repo public → minutes **illimitées**)
- **Cadence** : cron toutes les **15 minutes**, 24/7, sans interruption
- **Anti-doublon inter-exécutions** : l'état (`signal2_state.json`) est
  re-committé à la fin de chaque cycle (d'où les commits "state:")
- **Secrets** : le webhook Discord vit dans le secret `SIGNAL2_WEBHOOK`
  (jamais dans le code) ; `GITHUB_TOKEN` est fourni automatiquement par Actions
- **Coût** : 0 € — aucune API payante, aucune clé, aucun serveur

## 🔎 Les 4 couches de détection (aucune omission)

| Couche | Sources | Type de signal |
|---|---|---|
| 1. Officielle | RSS OpenAI, Mistral, Qwen, Google DeepMind + pages news Anthropic, xAI, DeepSeek | 🚀 sorties / 📰 annonces |
| 2. Hugging Face | 12 organisations suivies (nouveaux modèles publics) | 📦 poids ouverts |
| 3. GitHub | Évènements release de 10 organisations | 🏷️ releases |
| 4. Hacker News | Requêtes Algolia multi-mots-clés, score ≥ 40 | 📡 buzz fort (filet de sécurité) |

Fenêtre d'annonce : 7 jours — si le service subissait une interruption, il
**rattrape tout** au redémarrage (rien n'est perdu, rien n'est reposté).

## 🧪 Utilisation locale (développement uniquement)

```bash
python signal2.py --test        # cycle à blanc, aucun envoi
python signal2.py --poll        # un cycle réel
python signal2.py --digest 7    # digest rétrospectif
python signal2.py --launch-msg  # message de présentation
```

⚠️ Ne pas laisser tourner `--loop` en local en parallèle d'Actions :
l'état divergerait et provoquerait des doublons. Le repo est la source de vérité.

## 📁 Fichiers

- `signal2.py` — le système complet (stdlib uniquement, zéro dépendance)
- `signal2_config.json` — config locale (gitignored, contient le webhook)
- `signal2_state.json` — état persistant (suivi anti-doublon)
- `signal2.log` — journal (gitignored)

## 🛠️ Maintenance

- Ajouter un éditeur : listes `official_sources`, `hf_orgs`, `github_orgs`,
  `hn_queries` dans `signal2.py` (DEFAULT_CONFIG)
- Historique des veilles : onglet **Actions** du repo
- Vérifier un cycle : `gh run list --limit 5` puis `gh run view <id> --log`
