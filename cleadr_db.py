import sqlite3

conn = sqlite3.connect('school.db')
cursor = conn.cursor()

# Удаляем все тестовые объявления
cursor.execute("DELETE FROM announcements")
conn.commit()

print(f"Deleted {cursor.rowcount} announcements")
conn.close()