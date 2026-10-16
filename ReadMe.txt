python version should be lower than 3.11.
Because lib2to3 module is removed on python 3.12.
lib2to3 is used in GDAL.

# In windows, my development python version 3.9.13
# install gdal from gdal.whl in windows first and then install geopandas

Run following commands in cadastral DB:
create extension postgis
create extension dblink

*******************************
Cron Commands:
# crontab -e
# Run following py scripts on the 1st day of every month at 03:00 AM
# Add following commands to "crontab -e"
0 3 1 * * /work/by_on_the_fly/venv/bin/python /work/by_on_the_fly/run_cron_building.py >> /work/by_on_the_fly/logs/cron_building.log 2>&1
0 3 1 * * /work/by_on_the_fly/venv/bin/python /work/by_on_the_fly/run_cron_tn.py >> /work/by_on_the_fly/logs/cron_tn.log 2>&1
0 3 1 * * /work/by_on_the_fly/venv/bin/python /work/by_on_the_fly/run_cron_vertex.py >> /work/by_on_the_fly/logs/cron_vertex.log 2>&1


*******************************
Checking proxy valid or expired
Run following commands
# curl -x http://u5_175_254_135:Xsg0WmSCNHEVrlsL7XCEBMNU@5.175.254.135:37128 https://api.ipify.org
It returns ip addr if proxy is valid.