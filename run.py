from app import create_app
import threading

def run_admin_app():
    admin_app = create_app()
    admin_app.run(host='0.0.0.0', port=82, debug=True, use_reloader=False)

if __name__ == "__main__":
    # Start the admin app in a separate thread
    admin_thread = threading.Thread(target=run_admin_app, daemon=True)
    admin_thread.start()
    # Start the website module
    from app.plugins.website_module import WebsiteServer
    website = WebsiteServer()
    website.start()

    # Keep the main thread alive
    while True:
        pass