import cloudscraper

scraper = cloudscraper.create_scraper()

data_url = 'https://barttorvik.com/getadvstats.php?year=2026&specialSource=0&conyes=0&start=20251101&end=20260501&top=365&xvalue=All&page=playerstat&team='

response = scraper.get(data_url)
player_data = response.json()

print(f"Total players found: {len(player_data)}")

print("\n--- FIRST PLAYER RAW RECORDFILE ---")
print(player_data[0])

print("--- CLEANED PLAYER LIST ---")
for player in player_data[:20]:
    name = player[0]
    team = player[1]
    conference = player[2]
    year = player[22]
    height = player[23]
    print(f"Name: {name} | Team: {team} ({conference}) | Class: {year} | HT: {height}")
