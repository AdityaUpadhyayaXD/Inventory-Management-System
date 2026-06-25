from faker import Faker
import mysql.connector
import hashlib
import secrets
import random
import uuid

fake = Faker()

# ==========================
# DATABASE CONNECTION
# ==========================

conn = mysql.connector.connect(
    host="localhost",
    user="root",
    password="",  # Add password if needed
    database="inventory_db"
)

cursor = conn.cursor()

# ==========================
# CLEAR OLD DATA
# ==========================

cursor.execute("SET FOREIGN_KEY_CHECKS = 0")

try:
    cursor.execute("TRUNCATE TABLE stock_transactions")
except:
    pass

try:
    cursor.execute("TRUNCATE TABLE products")
except:
    pass

try:
    cursor.execute("TRUNCATE TABLE users")
except:
    pass

cursor.execute("SET FOREIGN_KEY_CHECKS = 1")

conn.commit()

# ==========================
# GENERATE USERS
# ==========================

def generate_users(count=100):
    inserted = 0
    attempts = 0
    max_attempts = count * 3  # avoid infinite loop

    while inserted < count and attempts < max_attempts:
        attempts += 1

        full_name = fake.name()

        # Use a wide random suffix (6 digits) to minimise collisions
        username = (
            full_name.lower()
            .replace(" ", "_")
            .replace(".", "")
            + str(random.randint(100000, 999999))
        )

        mobile_number = "9" + "".join(
            str(random.randint(0, 9))
            for _ in range(9)
        )

        password = "Password@123"
        password_salt = secrets.token_hex(16)
        password_hash = hashlib.sha512(
            (password + password_salt).encode()
        ).hexdigest()

        try:
            cursor.execute("""
                INSERT IGNORE INTO users
                (
                    username,
                    full_name,
                    mobile_number,
                    password_salt,
                    password_hash
                )
                VALUES (%s,%s,%s,%s,%s)
            """, (
                username,
                full_name,
                mobile_number,
                password_salt,
                password_hash
            ))
            if cursor.rowcount > 0:
                inserted += 1
        except mysql.connector.Error:
            pass  # skip duplicates silently

        # Commit in batches of 1000 for performance
        if inserted % 1000 == 0 and inserted > 0:
            conn.commit()

    conn.commit()
    print(f"{inserted} Users Inserted (requested {count})")


# ==========================
# GENERATE PRODUCTS
# ==========================

def generate_products(count=500):

    categories = [
        "Electronics",
        "Stationery",
        "Furniture",
        "Food",
        "Accessories",
        "Sports",
        "Books"
    ]

    product_names = [
        "Laptop",
        "Mouse",
        "Keyboard",
        "Monitor",
        "Printer",
        "USB Cable",
        "Router",
        "Notebook",
        "Pen",
        "Chair",
        "Table",
        "Headphones",
        "Speaker",
        "Power Bank"
    ]

    inserted = 0
    for _ in range(count):

        # uuid-based SKU guarantees uniqueness beyond 99,999
        sku = "SKU-" + uuid.uuid4().hex[:8].upper()

        name = random.choice(product_names)
        category = random.choice(categories)
        quantity = random.randint(1, 500)
        unit_price = round(random.uniform(50, 10000), 2)
        min_stock = random.randint(5, 50)
        supplier = fake.company()

        try:
            cursor.execute("""
                INSERT IGNORE INTO products
                (
                    sku,
                    name,
                    category,
                    quantity,
                    unit_price,
                    min_stock,
                    supplier
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s)
            """, (
                sku,
                name,
                category,
                quantity,
                unit_price,
                min_stock,
                supplier
            ))
            if cursor.rowcount > 0:
                inserted += 1
        except mysql.connector.Error:
            pass

        # Commit in batches of 5000 for performance
        if inserted % 5000 == 0 and inserted > 0:
            conn.commit()

    conn.commit()
    print(f"{inserted} Products Inserted (requested {count})")


# ==========================
# RUN EVERYTHING
# ==========================

try:
    generate_users(100)
    generate_products(500)

    print("\nFake data generated successfully!")

except mysql.connector.Error as e:
    print("MySQL Error:", e)

finally:
    cursor.close()
    conn.close()