def init_plugin(app):
    # Here, you can initialize the plugin
    # For example, add routes specific to the website module
    @app.route('/')
    def website_dashboard():
        return "Welcome to the Website Module"
