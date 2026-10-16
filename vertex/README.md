# vertex_detection
## 1) scrap images 
 - get cookie information wity by_get_cookie.py
 - run scrap.py to download images
 - image size is 4010 * 4010 (10 is delta added size to check boundary circles)
 - designed to scrap images within certain bbox. so it contains lots of empty requests.
 - empty requests put on wront_list.txt to skip on next scrapping.

## 2) check image result 
 - run command to get download files: find . -maxdepth 1 -type f -name "*.png" -printf "%f\n" > png_files.txt
 - python3 test.py to draw its result  
 
## 2) detect circles and insert into database
 - python3 main.py to detect circles and insert into database
 - once detect finished, it log into import_done.txt 
 - run python3 test.py to draw its import done result
