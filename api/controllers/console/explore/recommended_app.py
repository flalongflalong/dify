from flask_login import current_user
from flask_restful import Resource, fields, marshal_with, reqparse

from constants.languages import languages
from controllers.console import api
from controllers.console.wraps import account_initialization_required
from libs.login import login_required
from services.recommended_app_service import RecommendedAppService

app_fields = {
    'id': fields.String,
    'name': fields.String,
    'mode': fields.String,
    'icon': fields.String,
    'icon_background': fields.String
}

recommended_app_fields = {
    'app': fields.Nested(app_fields, attribute='app'),
    'app_id': fields.String,
    'description': fields.String(attribute='description'),
    'copyright': fields.String,
    'privacy_policy': fields.String,
    'category': fields.String,
    'position': fields.Integer,
    'is_listed': fields.Boolean
}

recommended_app_list_fields = {
    'recommended_apps': fields.List(fields.Nested(recommended_app_fields)),
    'categories': fields.List(fields.String)
}


class RecommendedAppListApi(Resource):
    @login_required
    @account_initialization_required
    @marshal_with(recommended_app_list_fields)
    def get(self):
        # language args
        parser = reqparse.RequestParser()
        parser.add_argument('language', type=str, location='args')
        args = parser.parse_args()

        if args.get('language') and args.get('language') in languages:
            language_prefix = args.get('language')
        elif current_user and current_user.interface_language:
            language_prefix = current_user.interface_language
        else:
            language_prefix = languages[0]

        if len(recommended_apps) == 0:
            recommended_apps = db.session.query(RecommendedApp).filter(
                RecommendedApp.is_listed == True,
                RecommendedApp.language == languages[0]
            ).all()

        categories = set()
        current_user.role = TenantService.get_user_role(current_user, current_user.current_tenant)
        recommended_apps_result = []
        for recommended_app in recommended_apps:
            installed = db.session.query(InstalledApp).filter(
                and_(
                    InstalledApp.app_id == recommended_app.app_id,
                    InstalledApp.tenant_id == current_user.current_tenant_id
                )
            ).first() is not None

            app = recommended_app.app
            if not app or not app.is_public:
                continue

            site = app.site
            if not site:
                continue

            recommended_app_result = {
                'id': recommended_app.id,
                'app': app,
                'app_id': recommended_app.app_id,
                'description': site.description,
                'copyright': site.copyright,
                'privacy_policy': site.privacy_policy,
                'category': recommended_app.category,
                'position': recommended_app.position,
                'is_listed': recommended_app.is_listed,
                'install_count': recommended_app.install_count,
                'installed': installed,
                'editable': current_user.role in ['owner', 'admin'],
                "is_agent": app.is_agent
            }
            recommended_apps_result.append(recommended_app_result)

            categories.add(recommended_app.category)  # add category to categories

        return {'recommended_apps': recommended_apps_result, 'categories': list(categories)}


class RecommendedAppApi(Resource):
    @login_required
    @account_initialization_required
    def get(self, app_id):
        app_id = str(app_id)
        return RecommendedAppService.get_recommend_app_detail(app_id)


api.add_resource(RecommendedAppListApi, '/explore/apps')
api.add_resource(RecommendedAppApi, '/explore/apps/<uuid:app_id>')
