import sqlite3


def init_db():
    # Connects to a database file (creates it if it doesn't exist)
    conn = sqlite3.connect('scouting_hub.db')
    cursor = conn.cursor()

    # Create a table to store your staff notes
    cursor.execute('''
                   CREATE TABLE IF NOT EXISTS player_notes (
                       player_name TEXT PRIMARY KEY,
                       team_name TEXT,
                       notes TEXT
                   )
                   ''')

    conn.commit()
    conn.close()


if __name__ == '__main__':
    init_db()
    print("Scouting database initialized successfully!")