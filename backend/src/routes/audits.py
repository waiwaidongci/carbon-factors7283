from fastapi import APIRouter,Depends
from src.config.database import database
from src.controllers.auth import current
from src.models.identity import Audit
from src.utils.serialize import row
from src.reconciliation.factory import get_store
router=APIRouter()
@router.get('/audits')
def audits(db=Depends(database),u=Depends(current)):
 business=[row(x) for x in db.query(Audit).order_by(Audit.id.desc()).limit(200).all()]
 try:
  recon=[{'id':10_000_000+a['id'],'at':a['at'],'actor':a['actor'],'action':a['action'],'detail':a['detail']} for a in get_store().audits()]
 except Exception:
  recon=[]
 return business+recon
