from .website_module import init_plugin  # Import the initialization function

def register(app):
    # This function will be called to initialize the plugin
    init_plugin(app)
    # You can add more setup or initialization for your plugin here if needed
    app.logger.info('Website module successfully initialized')
