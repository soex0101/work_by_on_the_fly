import sys
from labeling import db_crud
from read_settings import load_config


def main(run_id):

    cfg = load_config()
    dbname = f"{cfg.dbname_tn}_{run_id}"

    db_tn_params = {
        "dbname": dbname,
        "user": cfg.user,
        "password": cfg.password,
        "host": cfg.host,
        "port": cfg.port
    }

    connection_landkreis = db_crud.create_connection(db_tn_params)
    
    ########################################################################
    # Copy parcel nutzart info from TN db(landkreis) - it needs for labeling
    ########################################################################
    # Generate all_parcel_data in TN db
    query = f"""
    SELECT string_agg(
    format('SELECT gml_id, name, nutzart, bez, geom FROM %I WHERE geom && ST_MakeEnvelope({cfg.by_whole_bbox[0]}, {cfg.by_whole_bbox[1]}, {cfg.by_whole_bbox[2]}, {cfg.by_whole_bbox[3]}, 25832)', tablename),
    ' UNION ALL '
    )
    FROM pg_tables
    WHERE schemaname = 'public' AND tablename IN (
    SELECT table_name
    FROM information_schema.columns
    WHERE column_name = 'nutzart' AND table_schema = 'public'
    );
    """

    data = db_crud.run_query(connection_landkreis, query)
    union_query = data[0][0]

    # Handle empty result
    if not union_query:
        raise ValueError("No tables with 'nutzart' column found in tn database.")

    # Merge all table results to one table
    db_crud.drop_table_if_exist(connection_landkreis, "all_parcel_data")
    create_table_sql = f"""
    CREATE TABLE IF NOT EXISTS public.all_parcel_data (
        id SERIAL PRIMARY KEY,
        gml_id character varying,
        name character varying,
        nutzart character varying,
        bez character varying,
        geom geometry
    );
    """
    db_crud.run_query(connection_landkreis, create_table_sql)

    # Insert directly into the new table from union
    insert_sql = f"""
    INSERT INTO public.all_parcel_data (gml_id, name, nutzart, bez, geom)
    {union_query}
    """
    db_crud.run_query(connection_landkreis, insert_sql)

    print("Successfully created all_parcel_data table.")

    ########################################################
    # Generate all_merged_data table - it needs for labeling
    ########################################################

    # Generate union query string from all tables
    query = """
    SELECT string_agg(
    format('SELECT name, geom FROM %I WHERE nutzart = ''Straßenverkehr'' OR nutzart = ''Weg''', tablename),
    ' UNION ALL '
    )
    FROM pg_tables
    WHERE schemaname = 'public' AND tablename IN (
    SELECT table_name
    FROM information_schema.columns
    WHERE column_name = 'nutzart' AND table_schema = 'public'
    );
    """

    data = db_crud.run_query(connection_landkreis, query)
    union_query = data[0][0]

    # Handle empty result
    if not union_query:
        raise ValueError("No tables with 'nutzart' column found in landkreis database.")


    # Merge all tables to one table
    db_crud.drop_table_if_exist(connection_landkreis, "all_merged_data")
    create_table_sql = """
    CREATE TABLE IF NOT EXISTS public.all_merged_data (
        id SERIAL PRIMARY KEY,
        name character varying,
        geom geometry
    );
    """
    db_crud.run_query(connection_landkreis, create_table_sql)

    # Step 3: Insert directly into the new table from union
    insert_sql = f"""
    INSERT INTO public.all_merged_data (name, geom)
    {union_query}
    """
    db_crud.run_query(connection_landkreis, insert_sql)

    print("Successfully created all_merged_data table.")
    

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python 3_tn_to_cadastral.py <id>")
        print('Example command format: "python 3_tn_to_cadastral.py 20251113_050000"')
        sys.exit(1)

    run_id = sys.argv[1]

    main(run_id)