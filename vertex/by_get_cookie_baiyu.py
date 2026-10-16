from cgitb import reset

import selenium
from seleniumwire import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
from selenium.common.exceptions import NoSuchElementException, TimeoutException

import random
import time
import base64
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from read_settings import load_config
import common

cfg = load_config()

proxy_list = common.load_proxies()

username = cfg.atlas_username
password = cfg.atlas_password
# https://services.atlas.bayern.de/proxy/wms/map/a29d0fce-6901-43c0-90ed-ac73b7780479?REQUEST=GetMap&SERVICE=WMS&VERSION=1.1.1&FORMAT=image/png&STYLES=&TRANSPARENT=true&LAYERS=by_alkis_flurkarte_farbe&WIDTH=1920&HEIGHT=839&SRS=EPSG:3857&BBOX=1301260.181731689,6275535.267482367,1301833.4594438279,6275785.777899287


# URL to load
url = "https://atlas.bayern.de"


def access_shadow_root(driver, parent_element):
    return driver.execute_script("return arguments[0].shadowRoot", parent_element)


def get_cookie():
    print('getting MAP BY')
    print('==================')
    print(selenium.__version__)
    print('==================')
    # Set up Firefox options
    firefox_options = Options()
    firefox_options.binary_location = cfg.firefox_path
    firefox_options.add_argument("--disable-extensions")
    firefox_options.add_argument("--headless")
    firefox_options.add_argument("--width=1920")
    firefox_options.add_argument("--height=1080")
    firefox_options.set_preference("general.useragent.override",
                                   "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:134.0) Gecko/20100101 Firefox/134.0")

    # Set up the Firefox service
    # this works only selenium 4+
    firefox_service = Service(executable_path=cfg.gecko_path, log_path="geckodriver.log")
    # firefox_service = Service(executable_path="D:/geckodriver.exe")
    try:
        proxy_ip = random.choice(proxy_list)
        proxy_options = {
            'proxy': {
                'http': 'https://' + proxy_ip,
                'https': 'https://' + proxy_ip
            },
            'connection_timeout': 300
        }
        driver = webdriver.Firefox(service=firefox_service, seleniumwire_options=proxy_options, options=firefox_options)
        # driver = webdriver.Firefox(executable_path="/usr/local/bin/geckodriver", options=firefox_options)
        print('web driver connected---')
        # Open the URL
        driver.get(url)
        driver.set_window_size(1920, 2000)
        print('web driver set windows--')
        wait = WebDriverWait(driver, 30)
        shadow_host_outer = wait.until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "ba-navigation-rail"))
        )
        print('ba navigation -rail--')
        # Access the shadow root
        shadow_root_outer = access_shadow_root(driver, shadow_host_outer)

        # Locate the button within the shadow root
        button_element = shadow_root_outer.find_element(By.CSS_SELECTOR, "#authButton > span")
        print('button element--')
        # Scroll into view and click
        driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", button_element)
        button_element.click()
        print('button click--')
        # Wait for the inner shadow host to load inside the first shadow root
        shadow_host_modal = wait.until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "body > ba-modal"))
        )

        shadow_root_modal = access_shadow_root(driver, shadow_host_modal)
        print('pickup modal--')
        modal_container = shadow_root_modal.find_element(
            By.CSS_SELECTOR, "ba-auth-password-credential-panel"
        )

        # Access the second shadow root
        shadow_root_credential_panel = access_shadow_root(driver, modal_container)

        # Locate the input fields inside the second shadow root
        username_input = shadow_root_credential_panel.find_element(By.CSS_SELECTOR, "#credential_username")
        password_input = shadow_root_credential_panel.find_element(By.CSS_SELECTOR, "#credential_password")
        print('fields found')
        username_input.send_keys(username)
        password_input.send_keys(password)
        time.sleep(random.randint(5, 10))

        # Locate the "Authenticate Credential Button" shadow host
        shadow_host_button = shadow_root_credential_panel.find_element(By.CSS_SELECTOR,
                                                                       "#authenticate-credential-button")

        # Access the shadow root of the button's host
        shadow_root_button = access_shadow_root(driver, shadow_host_button)

        # Locate the button with the class "button primary" inside this shadow root
        authenticate_button = shadow_root_button.find_element(By.CSS_SELECTOR, "button.button.primary")
        print('authenticate_button found')

        actions = ActionChains(driver)
        actions.move_to_element(authenticate_button).perform()
        authenticate_button.click()
        time.sleep(random.randint(3, 4))

        # confirm login success
        logout_elements = shadow_root_outer.find_elements(By.CSS_SELECTOR, "#authButton.logout")
        if logout_elements:
            print('logout found - login success')

        # Access ba-navigation-rail
        shadow_host_navigation = wait.until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "ba-navigation-rail"))
        )
        shadow_navigation_outer = access_shadow_root(driver, shadow_host_navigation)

        home_button = shadow_navigation_outer.find_element(By.CSS_SELECTOR, "button")
        print('home_button found')
        time.sleep(random.randint(2, 3))

        actions.move_to_element(home_button).perform()
        home_button.click()

        # Access ba-header
        shadow_host_header = wait.until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "ba-header"))
        )
        shadow_header_outer = access_shadow_root(driver, shadow_host_header)

        # Wait until ba-header shadow host is present
        shadow_host_header = WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "ba-header"))
        )
        print("ba-header shadow host located.")

        # Access shadow root of ba-header
        shadow_header_outer = driver.execute_script("return arguments[0].shadowRoot", shadow_host_header)

        # Wait for the topic_button inside the shadow DOM
        topic_button = WebDriverWait(shadow_header_outer, 30).until(
            lambda d: shadow_header_outer.find_element(By.CSS_SELECTOR, "#topics_button")
        )
        print("topic_button located.")

        # Scroll the button into view
        driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", topic_button)
        print("Scrolled topic_button into view.")

        # Check if the button is enabled and displayed
        print(f"topic_button enabled: {topic_button.is_enabled()}")
        print(f"topic_button displayed: {topic_button.is_displayed()}")

        # Interact with the button
        if topic_button.is_displayed():
            # If the button is visible, use WebDriver actions
            actions.move_to_element(topic_button).perform()
            topic_button.click()
            print("topic_button clicked via WebDriver.")
        else:
            # If the button is not displayed, use JavaScript
            print("Using JavaScript click for topic_button.")
            driver.execute_script("arguments[0].click();", topic_button)
            print("topic_button clicked via JavaScript.")
            print("topic_button clicked.")

        time.sleep(random.randint(2, 3))

        # Access ba-main-menu once and reuse it
        shadow_host_main_menu = wait.until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "ba-main-menu"))
        )
        shadow_main_menu_outer = access_shadow_root(driver, shadow_host_main_menu)
        time.sleep(random.randint(1, 2))
        # Locate ba-topics-content-panel
        topics_content_panel = shadow_main_menu_outer.find_element(By.CSS_SELECTOR, "ba-topics-content-panel")
        shadow_topics_content = access_shadow_root(driver, topics_content_panel)
        time.sleep(random.randint(1, 2))

        # Locate the "plus_button" using its ID
        plus_button = shadow_topics_content.find_element(By.CSS_SELECTOR, "#button-plus")
        print('plus_button found')

        actions.move_to_element(plus_button).perform()
        plus_button.click()
        time.sleep(random.randint(1, 2))
        # Reuse shadow_main_menu_outer for further operations

        # Access ba-topics-content-panel in the active tab content
        topics_content_panel = shadow_main_menu_outer.find_element(
            By.CSS_SELECTOR, "#mainMenuContainer > div > div.tabcontent.is-active > ba-topics-content-panel"
        )
        shadow_topics_content = access_shadow_root(driver, topics_content_panel)

        # Locate ba-catalog-content-panel
        catalog_content_panel = shadow_topics_content.find_element(
            By.CSS_SELECTOR, "div > div:nth-child(2) > ba-catalog-content-panel:nth-child(8)"
        )
        shadow_catalog_content = access_shadow_root(driver, catalog_content_panel)
        time.sleep(random.randint(1, 2))
        # Locate ba-catalog-node:nth-child(5)
        catalog_node = shadow_catalog_content.find_element(By.CSS_SELECTOR, "ba-catalog-node:nth-child(5)")
        shadow_catalog_node = access_shadow_root(driver, catalog_node)

        # Click the list-item-button
        list_item_button = shadow_catalog_node.find_element(By.CSS_SELECTOR, "#list-item-button")
        print("list_item_button found.")
        actions.move_to_element(list_item_button).perform()
        list_item_button.click()
        print("list-item-button clicked.")

        # Locate ba-catalog-leaf:nth-child(6)
        catalog_leaf = shadow_catalog_node.find_element(By.CSS_SELECTOR,
                                                        "div > div > div > div:nth-child(6) > ba-catalog-leaf")
        shadow_catalog_leaf = access_shadow_root(driver, catalog_leaf)

        # Locate the checkbox with text "Bodenschätzung"
        checkbox = shadow_catalog_leaf.find_element(By.CSS_SELECTOR, "span > ba-checkbox > span")
        print("Checkbox 'Bodenschätzung' found.")

        # Verify the text of the checkbox
        checkbox_text = checkbox.get_attribute("textContent").strip()
        if checkbox_text != "Bodenschätzung":
            raise Exception(f"Unexpected checkbox text: {checkbox_text}")

        driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", checkbox)
        checkbox.click()
        print("Checkbox clicked!")
        time.sleep(random.randint(3, 5))

        # to zoom in try to use search box
        # Locate the input field within the shadow root of "ba-header"
        search_input = shadow_header_outer.find_element(By.CSS_SELECTOR, "#input")
        print("Search input field found.")

        search_input.send_keys('{0} {1}/{2}'.format("04301401", 0, 0))
        print("Search input field interacted with.")
        time.sleep(random.randint(3, 5))
        # Access ba-search-results-panel
        # search_results_panel = shadow_main_menu_outer.find_element(
        #     By.CSS_SELECTOR, "#mainMenuContainer > div > div.tabcontent.is-active > ba-search-results-panel"
        # )
        # shadow_search_results = access_shadow_root(driver, search_results_panel)
        #
        # # Locate ba-cp-results-panel
        # cp_results_panel = shadow_search_results.find_element(By.CSS_SELECTOR, "div > ba-cp-results-panel")
        # shadow_cp_results = access_shadow_root(driver, cp_results_panel)
        #
        # # Locate the first search result
        # first_search_result_item = shadow_cp_results.find_element(By.CSS_SELECTOR,
        #                                                           "div > div > ul > ba-search-content-panel-cp-item:nth-child(1)")
        # shadow_first_search_result = access_shadow_root(driver, first_search_result_item)
        # search_result_li = shadow_first_search_result.find_element(By.CSS_SELECTOR, "li")
        # print("First search result found.")
        # actions.move_to_element(search_result_li).perform()
        # search_result_li.click()
        print("First search result clicked.")
        time.sleep(random.randint(3, 5))
        cookies = ''
        foundErrorMsg = False
        for request in driver.requests:
            if request.response and request.response.body:
                response = str(request.response.body)
                if '<h1>Dienst nicht verfügbar</h1>' in response:
                    foundErrorMsg = True
                    break
            if request.method == 'GET' \
                    and 'Cookie' in request.headers:
                cookies = request.headers['Cookie']
            if "BA_AUTH_COOKIE=" in cookies:
                print("cookie found===========")
                break
            if foundErrorMsg:
                print("cookie not found===========, will try in 15mins")
                time.sleep(900)

        try:
            driver.close()
        except:
            pass
        try:
            driver.quit()
        except:
            pass
        return cookies

    except Exception as e:
        print('EXC BY', str(e))
        try:
            driver.close()
        except:
            pass
        try:
            driver.quit()
        except:
            pass
