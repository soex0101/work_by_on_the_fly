import psycopg2

# Global connections not shared across processes
def create_connection(db_params):
    try:
        # Connect to PostgreSQL
        conn = psycopg2.connect(**db_params)

        return conn
    except Exception as e:
        print(f"Error connectin to database: {e}")
        return None
    
def close_connection(conn):
    if conn:
        conn.close()

def run_update_query(conn, query, params = None):
    """
    Function to run an update query with parameters
    :param query: SQL query string
    :param params: Tuple of parameters for the query
    """
    cur = None
    try:
        cur = conn.cursor()

        # Execute the query
        cur.execute(query, params)

        # Commit the transaction
        conn.commit()

        #print("Update executed successfully!")

    except Exception as e:
        print(f"Error: {e}")
        print(f"    Query: {query}")
        if conn:
            conn.rollback()  # Rollback transaction in case of error

    finally:
        # Close cursor and connection if they were created
        if cur:
            cur.close()

def run_query(conn, query, params = None):
    """
    Function to run a query with parameters. Every statement is committed; on error the
    transaction is rolled back and the error is raised to the caller.
    :param query: SQL query string
    :param params: Tuple of parameters for the query
    :return: rows if the statement returns any (SELECT, WITH, ... RETURNING), otherwise None
    """
    with conn.cursor() as cur:
        try:
            # Execute the query
            cur.execute(query, params)

            # Decided by the statement itself, not its first word, so leading comments,
            # data-modifying WITH and ... RETURNING are handled too
            rows = cur.fetchall() if cur.description is not None else None

            # Commit every statement, SELECT included, so the connection never stays
            # "idle in transaction" holding locks on everything it has read
            conn.commit()

        except Exception as e:
            print(f"Error: {e}")
            print(f"    Query: {query}")
            try:
                conn.rollback()  # keep the connection usable for the caller
            except psycopg2.Error:
                pass  # connection is broken; the original error is the one that matters
            raise

    return rows

def drop_table_if_exist(conn, table_name):
    query = f"DROP TABLE IF EXISTS {table_name} CASCADE"

    run_query(conn, query)

def copy_table_with_bbox(conn, src_table_name, dest_table_name, bbox, geom_field = 'geom'):
    query = f"CREATE TABLE {dest_table_name} AS SELECT * FROM {src_table_name} WHERE {geom_field} && ST_MakeEnvelope(%s, %s, %s, %s, 25832)"
    run_query(conn, query, bbox)

    
def get_table_row_count(conn, table_name):
    query = f"SELECT COUNT(*) FROM {table_name}"
    data = run_query(conn, query)
    return data[0][0]